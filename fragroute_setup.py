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
    {"key": "llm_fast", "label": "Coach LLM (Qwen2.5-1.5B, in-game)", "folder": "llm",
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
    {"key": "voice", "label": "Voice STT (whisper base.en)", "folder": "stt",
     "filename": "ggml-base.en.bin", "approxMB": 142, "required": False,
     "sha256": "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
     "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"},
    {"key": "ffmpeg", "label": "Recorder/Video (ffmpeg LGPL)", "folder": ".",
     "filename": "ffmpeg.exe", "approxMB": 110, "required": True, "kind": "zip_ffmpeg",
     "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip"},
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


def status():
    out = []
    for it in MANIFEST:
        pr = _PROG.get(it["key"]) or {}
        out.append({"key": it["key"], "label": it["label"], "approxMB": it["approxMB"],
                    "required": it.get("required", False), "present": is_present(it),
                    "status": pr.get("status", ""), "pct": pr.get("pct", 0)})
    missingMB = sum(i["approxMB"] for i in MANIFEST if not is_present(i))
    return {"build": APP_SETUP_BUILD, "items": out, "running": _RUNNING["on"],
            "missingMB": missingMB, "present": sum(1 for i in MANIFEST if is_present(i)),
            "total": len(MANIFEST)}


def _download(item):
    key = item["key"]
    dest = _dest(item)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _PROG[key] = {"status": "downloading", "pct": 0, "mb": 0, "totalMb": item["approxMB"]}
    is_zip = item.get("kind") == "zip_ffmpeg"
    tmp = str(dest) + (".zipdl" if is_zip else ".part")
    try:
        req = urllib.request.Request(item["url"], headers={"User-Agent": "FRAGROUTE-setup"})
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
