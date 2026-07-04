"""FRAGROUTE first-run model installer -- downloads the big AI sidecars a buyer
can't ship inside a ~90MB exe. Pure stdlib (urllib). Streams with progress, places
files in the right sidecar folders, extracts the ffmpeg zip. The runtime BINARIES
(llama.cpp / sd.cpp / whisper.cpp / sd-cli) + the CLIP onnx are shipped WITH the
app package (small / generated), so they're not in this manifest.

Engine sets BASE_DIR (the folder that holds llm/ sd/ yolo/ stt/ + the exe).
"""
import os
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

APP_SETUP_BUILD = "setup-1"

BASE_DIR = None
_LOCK = threading.Lock()
_PROG = {}            # key -> {status, pct, mb, totalMb}
_RUNNING = {"on": False}

# Each: key, label, folder (relative to BASE_DIR), filename, url, approxMB, kind, required
MANIFEST = [
    {"key": "llm_smart", "label": "Coach LLM (Qwen2.5-14B, smart)", "folder": "llm",
     "filename": "Qwen2.5-14B-Instruct-Q4_K_M.gguf", "approxMB": 8990, "required": False,
     "url": "https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf"},
    {"key": "llm_mid", "label": "Coach LLM (Phi-3.5-mini, in-game -- smarter, fits 4GB)", "folder": "llm",
     "filename": "Phi-3.5-mini-instruct-Q4_K_M.gguf", "approxMB": 2282, "required": False,
     "sha256": "e4165e3a71af97f1b4820da61079826d8752a2088e313af0c7d346796c38eff5",
     "url": "https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF/resolve/main/Phi-3.5-mini-instruct-Q4_K_M.gguf"},
    {"key": "llm_fast", "label": "Coach LLM (Qwen2.5-1.5B, lightweight in-game)", "folder": "llm",
     "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf", "approxMB": 1120, "required": True,
     "sha256": "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e",
     "url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"},
    {"key": "vision", "label": "Vision model (Qwen2-VL-2B)", "folder": "llm",
     "filename": "Qwen2-VL-2B-Instruct-Q4_K_M.gguf", "approxMB": 990, "required": False,
     "sha256": "5745685d2e607a82a0696c1118e56a2a1ae0901da450fd9cd4f161c6b62867d7",
     "url": "https://huggingface.co/ggml-org/Qwen2-VL-2B-Instruct-GGUF/resolve/main/Qwen2-VL-2B-Instruct-Q4_K_M.gguf"},
    {"key": "vision_mmproj", "label": "Vision projector (mmproj)", "folder": "llm",
     "filename": "mmproj-Qwen2-VL-2B-Instruct-f16.gguf", "approxMB": 1330, "required": False,
     "sha256": "ecb20cabcdd8dbc277de06bd6eb980aeb2adfaaba9f199a434e328d205675d03",
     "url": "https://huggingface.co/ggml-org/Qwen2-VL-2B-Instruct-GGUF/resolve/main/mmproj-Qwen2-VL-2B-Instruct-f16.gguf"},
    {"key": "imagegen", "label": "Image gen (SDXL base 1.0)", "folder": "sd",
     "filename": "sd_xl_base_1.0.safetensors", "approxMB": 6617, "required": False,
     "sha256": "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b",
     "url": "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors"},
    {"key": "detector", "label": "Detector base (YOLOX-tiny, generic)", "folder": "yolo",
     "filename": "yolox_tiny.onnx", "approxMB": 20, "required": False,
     "sha256": "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
     "url": "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx"},
    {"key": "voice", "label": "Voice STT (whisper small.en -- great freeform speech)", "folder": "stt",
     "filename": "ggml-small.en.bin", "approxMB": 466, "required": False,
     "sha256": "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
     "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin"},
    # FAST voice model: the persistent whisper-server (fragroute_voice.find_fast_model)
    # prefers base.en for low-latency conversational voice (~3x faster decode than
    # small.en -- verified 4.5s->2.0s per turn). Without this, a fresh buyer install
    # only has small.en and voice replies are noticeably slower. No embedded hash yet
    # (not independently re-verified against the upstream file) -> size-check only,
    # same as the 14B/ffmpeg entries below.
    {"key": "voice_fast", "label": "Voice STT -- fast (whisper base.en, snappy replies)", "folder": "stt",
     "filename": "ggml-base.en.bin", "approxMB": 141, "required": False,
     "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"},
    {"key": "ffmpeg", "label": "Recorder/Video (ffmpeg LGPL)", "folder": ".",
     "filename": "ffmpeg.exe", "approxMB": 110, "required": True, "kind": "zip_ffmpeg",
     "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip"},
    # Offline IP->city database so the Live Game tab can name ANY server (incl.
    # off-VPN / non-Alibaba-LB raw match IPs), no account, no online lookup.
    # DB-IP City Lite is CC BY 4.0 (commercial OK with attribution -- see NOTICES).
    # The free build is published monthly, so the URL carries YYYY-MM (resolved at
    # download time, with a fallback to the previous month if this month isn't up yet).
    {"key": "geoip", "label": "Server locator (DB-IP City Lite, off-VPN names)", "folder": "geo",
     "filename": "dbip-city-lite.mmdb", "approxMB": 55, "required": False, "kind": "gz_monthly",
     "url": "https://download.db-ip.com/free/dbip-city-lite-{ym}.mmdb.gz"},
]


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _base():
    if BASE_DIR:
        return Path(BASE_DIR)
    import sys
    return (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent)


def _dest(item):
    return _base() / item["folder"] / item["filename"]


def is_present(item):
    p = _dest(item)
    try:
        # present + at least 60% of expected size (guards truncated downloads)
        return p.exists() and p.stat().st_size >= item.get("approxMB", 0) * 1048576 * 0.6
    except Exception:
        return False


def recommend(prof=None):
    """Match the model catalog to THIS PC's hardware: which tier fits, and the
    recommended default download set. A 4GB card gets Phi-3.5 (not the 14B) and skips
    SDXL; a 12GB+ card gets the 14B + SDXL; a no-GPU box gets the 1.5B on CPU. This is
    how 'we scan the hardware -> we know which model size fits' becomes concrete."""
    try:
        import fragroute_hardware
        p = prof or fragroute_hardware.detect()
    except Exception:
        p = prof or {}
    vram = p.get("bestVramGB") or 0
    ram = p.get("ramGB") or 0
    # 'smart' coach LLM (runs out-of-game) sized to the card:
    if vram >= 10:   smart = "llm_smart"    # Qwen2.5-14B
    elif vram >= 4:  smart = "llm_mid"      # Phi-3.5-mini
    else:            smart = "llm_fast"     # Qwen2.5-1.5B (CPU-friendly)
    imagegen_ok = vram >= 8                 # SDXL is heavy; crawls under ~8GB
    rec = {smart, "llm_fast", "vision", "vision_mmproj", "detector", "voice"}
    if imagegen_ok:
        rec.add("imagegen")
    fit = {}
    for it in MANIFEST:
        k = it["key"]
        if k == "llm_smart":   fit[k] = "good" if vram >= 10 else ("heavy" if vram >= 6 else "toobig")
        elif k == "llm_mid":   fit[k] = "good" if vram >= 4 else "heavy"
        elif k == "imagegen":  fit[k] = "good" if imagegen_ok else "heavy"
        else:                  fit[k] = "good"
    gpu = (p.get("primaryGpu") or {}).get("name") or "Your GPU"
    if vram >= 10:   note = "%s (%gGB) runs the smart 14B coach + SDXL image gen." % (gpu, vram)
    elif vram >= 8:  note = "%s (%gGB) runs Phi-3.5 + SDXL image gen." % (gpu, vram)
    elif vram >= 4:  note = "%s (%gGB) runs Phi-3.5; SDXL image gen needs 8GB+ (skipped -- would be slow)." % (gpu, vram)
    elif vram > 0:   note = "%s (%gGB) runs the lightweight 1.5B coach; heavy AI features will be slow." % (gpu, vram)
    else:            note = "No GPU detected -- the 1.5B coach runs on CPU; image gen/vision will be slow."
    return {"vramGB": vram, "ramGB": ram, "smartLlm": smart,
            "recommendedKeys": sorted(rec), "fit": fit, "note": note}


def status():
    rec = recommend()
    fit = rec.get("fit", {}); recset = set(rec.get("recommendedKeys", []))
    out = []
    for it in MANIFEST:
        pr = _PROG.get(it["key"]) or {}
        out.append({"key": it["key"], "label": it["label"], "approxMB": it["approxMB"],
                    "required": it.get("required", False), "present": is_present(it),
                    "fit": fit.get(it["key"], "good"), "recommended": it["key"] in recset,
                    "status": pr.get("status", ""), "pct": pr.get("pct", 0)})
    missingMB = sum(i["approxMB"] for i in MANIFEST if not is_present(i))
    recMissingMB = sum(i["approxMB"] for i in MANIFEST if i["key"] in recset and not is_present(i))
    return {"build": APP_SETUP_BUILD, "items": out, "running": _RUNNING["on"],
            "missingMB": missingMB, "recMissingMB": recMissingMB,
            "recommendedKeys": sorted(recset), "hardwareNote": rec.get("note"),
            "present": sum(1 for i in MANIFEST if is_present(i)),
            "total": len(MANIFEST)}


def _resolve_monthly_url(template):
    """Fill {ym} with a recent YYYY-MM that actually exists. DB-IP publishes the
    free DB monthly; early in a month the new file may not be up yet, so we try
    this month and fall back through the previous two."""
    import datetime
    now = datetime.datetime.utcnow()
    for back in range(0, 3):
        y, m = now.year, now.month - back
        while m <= 0:
            m += 12
            y -= 1
        url = template.replace("{ym}", "%04d-%02d" % (y, m))
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "FRAGROUTE-setup"})
            with urllib.request.urlopen(req, timeout=20) as r:
                if getattr(r, "status", 200) == 200:
                    return url
        except Exception:
            continue
    return None


def _download(item):
    key = item["key"]
    dest = _dest(item)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _PROG[key] = {"status": "downloading", "pct": 0, "mb": 0, "totalMb": item["approxMB"]}
    kind = item.get("kind", "")
    is_zip = kind == "zip_ffmpeg"
    is_gz = kind == "gz_monthly"
    url = item["url"]
    if is_gz:
        url = _resolve_monthly_url(item["url"])
        if not url:
            _PROG[key] = {"status": "error: database not available yet, try later", "pct": 0}
            return False
    tmp = str(dest) + (".gz" if is_gz else ".zipdl" if is_zip else ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FRAGROUTE-setup"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length", 0) or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)      # 1MB
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(100 * done / total) if total else 0
                    _PROG[key] = {"status": "downloading", "pct": pct,
                                  "mb": round(done / 1048576), "totalMb": round((total or item["approxMB"] * 1048576) / 1048576)}
        if is_zip:
            _PROG[key]["status"] = "extracting"
            with zipfile.ZipFile(tmp) as z:
                member = next((n for n in z.namelist() if n.lower().endswith("/bin/ffmpeg.exe") or n.lower().endswith("ffmpeg.exe")), None)
                if not member:
                    raise RuntimeError("ffmpeg.exe not found in zip")
                with z.open(member) as src, open(dest, "wb") as out:
                    while True:
                        b = src.read(1 << 20)
                        if not b:
                            break
                        out.write(b)
            os.remove(tmp)
        elif is_gz:
            _PROG[key]["status"] = "extracting"
            import gzip
            import shutil
            with gzip.open(tmp, "rb") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.remove(tmp)
        else:
            os.replace(tmp, dest)
        # integrity check against the known-good hash (where we have one)
        want = item.get("sha256")
        if want:
            _PROG[key] = {"status": "verifying", "pct": 100, "mb": _PROG[key].get("mb", 0), "totalMb": item["approxMB"]}
            try:
                got = _sha256(dest)
            except Exception as e:
                got = "err:%s" % e
            if got != want:
                try:
                    os.remove(dest)
                except Exception:
                    pass
                _PROG[key] = {"status": "error: checksum mismatch (re-download)", "pct": 0}
                return False
        _PROG[key] = {"status": "done", "pct": 100, "mb": _PROG[key].get("mb", 0), "totalMb": item["approxMB"]}
        return True
    except Exception as e:
        _PROG[key] = {"status": "error: %s" % str(e)[:80], "pct": 0}
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def download(keys=None):
    """Download the given keys (or all MISSING) in a background thread. Returns
    immediately; poll status() for progress. One run at a time."""
    with _LOCK:
        if _RUNNING["on"]:
            return {"ok": False, "message": "a download is already running"}
        _RUNNING["on"] = True

    def _work():
        try:
            if keys:
                items = [i for i in MANIFEST if i["key"] in keys]
            else:
                items = [i for i in MANIFEST if not is_present(i)]
            for it in items:
                if not is_present(it):
                    _download(it)
        finally:
            _RUNNING["on"] = False
    threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "started": True}
